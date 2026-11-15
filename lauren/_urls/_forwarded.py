"""Trusted reverse proxies: ``ProxyConfig`` and the forwarding-header resolver (private).

:func:`resolve_forwarded` is a pure function of the immediate peer address,
the request headers and the compiled config. It answers ``None`` — use the
values the server reported — unless the peer is a trusted proxy. For a
trusted peer it walks the forwarded-for list right to left (each proxy
appends the address it received the request from), skipping trusted
addresses for at most ``max_hops`` entries; the first untrusted entry is the
client. Scheme, host, port and prefix come from the same hop, so a value the
client wrote into the header itself (the leftmost entries) is never used
unless every proxy in between is trusted. Invalid values are ignored (logged
at debug level) — a request never fails because of a forwarding header.
"""

from __future__ import annotations

import ipaddress
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal

from ..exceptions import StartupError
from ._url import parse_authority

logger = logging.getLogger("lauren.proxy")

#: ``ProxyConfig.headers`` values.
HEADER_MODES: tuple[str, ...] = ("x-forwarded", "forwarded", "both")

_HTTP_SCHEMES = {"http": "http", "https": "https"}
_WS_SCHEMES = {"http": "ws", "https": "wss", "ws": "ws", "wss": "wss"}


@dataclass(frozen=True, slots=True, kw_only=True)
class ProxyConfig:
    """Which reverse proxies Lauren trusts, and which forwarding headers it reads.

    Pass it as ``LaurenFactory.create(..., proxy=ProxyConfig(trusted=[...]))``.
    Forwarding headers are honoured **only** when the immediate peer
    (``scope["client"]``) is in ``trusted``; from anyone else they are
    ignored. The resolved client, scheme, host and root path then appear on
    ``request.client`` / ``scheme`` / ``host`` / ``root_path`` (and the
    WebSocket equivalents) for every middleware, guard and handler.

    * ``trusted`` — addresses and CIDR networks (``"10.0.0.0/8"``,
      ``"127.0.0.1"``, ``"::1"``), ``"unix"`` for Unix-socket peers, or
      ``"*"`` for every peer (logged as a warning: the headers are then
      client-controlled).
    * ``headers`` — ``"x-forwarded"`` (``X-Forwarded-For`` / ``-Proto`` /
      ``-Host`` / ``-Port`` / ``-Prefix``), ``"forwarded"`` (RFC 7239
      ``Forwarded``) or ``"both"`` (``Forwarded`` when present).
    * ``max_hops`` — how many forwarded-for entries may be walked (one per
      proxy in front of the app).
    * ``trust_prefix`` — honour ``X-Forwarded-Prefix`` as the public path
      prefix (it is prepended to ``request.root_path``).
    * ``server_applied`` — the ASGI server already rewrote ``client`` and
      ``scheme`` (``uvicorn --proxy-headers``); Lauren then derives only the
      host and prefix.
    """

    trusted: Sequence[str] | str
    headers: Literal["x-forwarded", "forwarded", "both"] = "x-forwarded"
    max_hops: int = 1
    trust_prefix: bool = False
    server_applied: bool = False


@dataclass(frozen=True, slots=True)
class Forwarded:
    """What a trusted proxy said about one request (``None``: keep the server's value)."""

    client: tuple[str, int | None] | None = None
    scheme: str | None = None
    #: Validated ``host[:port]`` authority.
    host: str | None = None
    #: Forwarded port (``X-Forwarded-Port``); applies to the forwarded or the request's own host.
    port: int | None = None
    #: Validated path prefix without a trailing slash (``"/api"``).
    prefix: str | None = None


_Network = ipaddress.IPv4Network | ipaddress.IPv6Network


class CompiledProxy:
    """A validated :class:`ProxyConfig` (networks parsed once, at startup)."""

    __slots__ = ("networks", "trust_unix", "trust_all", "mode", "max_hops", "trust_prefix", "server_applied")

    def __init__(
        self,
        networks: tuple[_Network, ...],
        *,
        trust_unix: bool,
        trust_all: bool,
        mode: str,
        max_hops: int,
        trust_prefix: bool,
        server_applied: bool,
    ) -> None:
        self.networks = networks
        self.trust_unix = trust_unix
        self.trust_all = trust_all
        self.mode = mode
        self.max_hops = max_hops
        self.trust_prefix = trust_prefix
        self.server_applied = server_applied

    def is_trusted(self, host: str | None) -> bool:
        """Whether a peer / hop address belongs to a trusted proxy."""
        if not host or host == "unix":
            return self.trust_unix or self.trust_all
        if self.trust_all:
            return True
        address = _ip(host)
        if address is None:
            return False
        if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
            address = address.ipv4_mapped
        return any(address in network for network in self.networks)


def _ip(text: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(text)
    except ValueError:
        return None


def _invalid(field: str, value: Any, why: str) -> StartupError:
    return StartupError(
        f"ProxyConfig({field}={value!r}) is invalid: {why}",
        detail={"reason": "invalid_proxy_config", "field": field},
    )


def compile_proxy(config: Any) -> CompiledProxy:
    """Validate ``proxy=`` (``StartupError``, ``reason="invalid_proxy_config"``)."""
    if not isinstance(config, ProxyConfig):
        raise StartupError(
            f"proxy= expects a ProxyConfig, got {type(config).__name__}",
            detail={"reason": "invalid_proxy_config", "got": type(config).__name__},
        )
    entries = [config.trusted] if isinstance(config.trusted, str) else config.trusted
    if not isinstance(entries, (list, tuple, set, frozenset)) or not entries:
        raise _invalid("trusted", config.trusted, "name at least one trusted proxy address or network")
    networks: list[_Network] = []
    trust_unix = trust_all = False
    for entry in entries:
        if not isinstance(entry, str):
            raise _invalid("trusted", entry, "entries must be strings")
        if entry == "unix":
            trust_unix = True
        elif entry == "*":
            trust_all = True
        else:
            try:
                networks.append(ipaddress.ip_network(entry, strict=False))
            except ValueError:
                raise _invalid(
                    "trusted", entry, "expected an IP address, a CIDR network, 'unix' or '*'"
                ) from None
    if config.headers not in HEADER_MODES:
        raise _invalid("headers", config.headers, f"expected one of {', '.join(map(repr, HEADER_MODES))}")
    if isinstance(config.max_hops, bool) or not isinstance(config.max_hops, int) or config.max_hops < 1:
        raise _invalid("max_hops", config.max_hops, "expected an int >= 1")
    for flag in ("trust_prefix", "server_applied"):
        if not isinstance(getattr(config, flag), bool):
            raise _invalid(flag, getattr(config, flag), "expected a bool")
    return CompiledProxy(
        tuple(networks),
        trust_unix=trust_unix,
        trust_all=trust_all,
        mode=config.headers,
        max_hops=config.max_hops,
        trust_prefix=config.trust_prefix,
        server_applied=config.server_applied,
    )


# ---------------------------------------------------------------------------
# Header parsing
# ---------------------------------------------------------------------------


def _split_list(values: list[str]) -> list[str]:
    """Comma-separated values of every occurrence of a header, in order."""
    out: list[str] = []
    for value in values:
        out.extend(part.strip() for part in value.split(","))
    return out


def _node(value: str) -> tuple[str, int | None]:
    """Split a forwarded-for node (``ip``, ``ip:port``, ``[v6]:port``, ``unknown``, ``_hidden``)."""
    if value.startswith("["):
        end = value.find("]")
        if end > 0:
            host, rest = value[: end + 1], value[end + 1 :]
            port = _port(rest[1:]) if rest.startswith(":") else None
            return host[1:-1], port
        return value, None
    if value.count(":") == 1:
        host, _, port_text = value.partition(":")
        return host, _port(port_text)
    return value, None


def _parse_forwarded(values: list[str]) -> list[dict[str, str]] | None:
    """Parse RFC 7239 ``Forwarded`` elements; ``None`` when the header is malformed."""
    text = ",".join(values)
    elements: list[dict[str, str]] = []
    current: dict[str, str] = {}
    i, n = 0, len(text)
    while i < n:
        while i < n and text[i] in " \t":
            i += 1
        start = i
        while i < n and text[i] not in "=;, \t":
            i += 1
        key = text[start:i].lower()
        if not key or i >= n or text[i] != "=":
            return None
        i += 1
        if i < n and text[i] == '"':
            i += 1
            chars: list[str] = []
            while i < n and text[i] != '"':
                if text[i] == "\\" and i + 1 < n:
                    i += 1
                chars.append(text[i])
                i += 1
            if i >= n:
                return None
            i += 1
            value = "".join(chars)
        else:
            start = i
            while i < n and text[i] not in ";, \t":
                i += 1
            value = text[start:i]
        current.setdefault(key, value)
        while i < n and text[i] in " \t":
            i += 1
        if i < n and text[i] == ";":
            i += 1
        elif i < n and text[i] == ",":
            elements.append(current)
            current = {}
            i += 1
        elif i < n:
            return None
    if current:
        elements.append(current)
    return elements


def _walk(proxy: CompiledProxy, hops: list[str]) -> int:
    """Index of the client hop: right to left, past trusted hops, at most ``max_hops``."""
    walked = 0
    for index in range(len(hops) - 1, -1, -1):
        walked += 1
        host, _ = _node(hops[index])
        if walked >= proxy.max_hops or not host or not proxy.is_trusted(host):
            return index
    return 0


def _pick(values: list[str], offset: int) -> str | None:
    """The value a proxy wrote for the hop ``offset`` places from the right."""
    values = [value for value in values if value]
    if not values:
        return None
    # A shorter list has no value attributable to the selected hop.
    # Falling back to its leftmost value would trust client-injected data
    # when an interior proxy appended XFF but did not append -Proto/-Host.
    return values[-offset] if len(values) >= offset else None


def _scheme(value: str | None, websocket: bool) -> str | None:
    if value is None:
        return None
    scheme = (_WS_SCHEMES if websocket else _HTTP_SCHEMES).get(value.lower())
    if scheme is None:
        logger.debug("ignored forwarded scheme %r", value)
    return scheme


def _host(value: str | None) -> str | None:
    if value is None:
        return None
    if parse_authority(value) is None:
        logger.debug("ignored forwarded host %r", value)
        return None
    return value


def _port(value: str | None) -> int | None:
    if value is None:
        return None
    if len(value) > 5 or not value.isdigit() or not 0 < int(value) < 65536:
        logger.debug("ignored forwarded port %r", value)
        return None
    return int(value)


def _prefix(value: str | None) -> str | None:
    if value is None:
        return None
    prefix = value.rstrip("/")
    if (
        not value.startswith("/")
        or value.startswith("//")
        or any(ch in value for ch in "?#\\")
        or any(ord(ch) < 0x21 or ord(ch) == 0x7F for ch in value)
        or any(part in (".", "..") for part in prefix.split("/"))
    ):
        logger.debug("ignored forwarded prefix %r", value)
        return None
    return prefix


def resolve_forwarded(
    proxy: CompiledProxy, peer: str | None, headers: Any, *, websocket: bool = False
) -> Forwarded | None:
    """What a trusted proxy forwarded for this request, or ``None`` (peer untrusted / nothing sent)."""
    if not proxy.is_trusted(peer):
        return None
    client: tuple[str, int | None] | None = None
    scheme_value: str | None = None
    host_value: str | None = None
    port_value: str | None = None
    prefix_value: str | None = None
    elements: list[dict[str, str]] | None = None
    if proxy.mode != "x-forwarded":
        raw = headers.getall("forwarded")
        if raw:
            elements = _parse_forwarded(raw)
            if elements is None:
                logger.debug("ignored malformed Forwarded header %r", raw)
                # ``both`` means prefer Forwarded *when present*. Falling
                # back to X-Forwarded-* here could promote client-supplied
                # headers from a peer that only appends RFC 7239 Forwarded.
                return None
    if elements:
        index = _walk(proxy, [element.get("for", "") for element in elements])
        element = elements[index]
        if "for" in element:
            client = _node(element["for"])
        scheme_value = element.get("proto")
        host_value = element.get("host")
        offset = len(elements) - index
    elif proxy.mode != "forwarded":
        hops = [hop for hop in _split_list(headers.getall("x-forwarded-for")) if hop]
        offset = 1
        if hops:
            index = _walk(proxy, hops)
            client = _node(hops[index])
            offset = len(hops) - index
        scheme_value = _pick(_split_list(headers.getall("x-forwarded-proto")), offset)
        host_value = _pick(_split_list(headers.getall("x-forwarded-host")), offset)
        port_value = _pick(_split_list(headers.getall("x-forwarded-port")), offset)
    else:
        offset = 1
    if proxy.trust_prefix:
        prefix_value = _pick(_split_list(headers.getall("x-forwarded-prefix")), offset)
    if proxy.server_applied:
        client, scheme_value = None, None
    result = Forwarded(
        client=client,
        scheme=_scheme(scheme_value, websocket),
        host=_host(host_value),
        port=_port(port_value),
        prefix=_prefix(prefix_value),
    )
    if result == _NOTHING:
        return None
    return result


_NOTHING = Forwarded()


__all__ = ["CompiledProxy", "Forwarded", "HEADER_MODES", "ProxyConfig", "compile_proxy", "resolve_forwarded"]

"""``LaurenFactory.create`` hooks: URL options, the name registry, guard checks (private).

* :func:`prepare_urls` (before Phase 2) validates ``proxy=``,
  ``public_base_url=`` and ``url_signing=`` and builds the app's
  :class:`UrlBuilder`;
* :func:`install_urls` (Phase 2) registers it as a global provider;
* :func:`build_route_table` (end of Phase 5) names every HTTP route and
  WebSocket gateway, checks uniqueness and binds the table into the builder;
* :func:`check_signature_guards` (Phase 5b) refuses ``ValidSignature``
  without ``url_signing=``.
"""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from ..exceptions import StartupError
from ._forwarded import CompiledProxy, compile_proxy
from ._registry import RouteSpec, RouteTable, UrlBuilder, split_base_url
from ._signing import UrlSigner

#: Class attribute marking ``Lauren``'s synthetic controller: its routes'
#: derived names are the bare function names (FastAPI style).
APP_ROUTES_FLAG = "__lauren_app_routes__"

#: Class attribute of guards that verify signed URLs (``ValidSignature`` and
#: its subclasses); such a guard needs ``url_signing=``.
REQUIRES_SIGNING_FLAG = "__lauren_requires_url_signing__"


@dataclass(frozen=True, slots=True)
class UrlSetup:
    """The validated URL options of one app."""

    builder: UrlBuilder
    proxy: CompiledProxy | None
    signing: bool


def prepare_urls(
    *, proxy: Any, public_base_url: Any, url_signing: Any, root_path: str, logger: Any, raw_path: bool = False
) -> UrlSetup:
    """Validate the URL-related factory options (``StartupError`` with a ``reason``)."""
    compiled = compile_proxy(proxy) if proxy is not None else None
    if compiled is not None and compiled.trust_all:
        message = (
            'ProxyConfig(trusted=["*"]): every peer is trusted, so forwarding headers are '
            "client-controlled; list your proxies' addresses instead"
        )
        logging.getLogger("lauren").warning(message)
        logger.warn(message, context="LaurenFactory")
    if public_base_url is not None:
        try:
            split_base_url(public_base_url)
        except ValueError as exc:
            raise StartupError(
                f"public_base_url={public_base_url!r} is invalid: {exc}",
                detail={"reason": "invalid_public_base_url", "got": repr(public_base_url)},
            ) from None
    signer = (
        UrlSigner(url_signing, has_origin=bool(public_base_url), raw_path=raw_path)
        if url_signing is not None
        else None
    )
    builder = UrlBuilder(public_base_url=public_base_url or None, root_path=root_path, signer=signer)
    return UrlSetup(builder=builder, proxy=compiled, signing=signer is not None)


def install_urls(container: Any, setup: UrlSetup) -> None:
    """Phase 2: make the app's :class:`UrlBuilder` injectable everywhere."""
    if container.has_provider(UrlBuilder):
        # All request helpers use the app-owned builder; allowing a separate
        # injectable instance would make services generate different URLs.
        raise StartupError(
            "UrlBuilder is managed by LaurenFactory.create; do not register another provider for it",
            detail={"reason": "url_builder_override"},
        )
    container.register_value(UrlBuilder, setup.builder)


def check_signature_guards(uses: Iterable[tuple[Any, str]], setup: UrlSetup) -> None:
    """Phase 5b: a signed-URL guard on a route or gateway needs ``url_signing=``."""
    if setup.signing:
        return
    for guard, where in uses:
        if getattr(guard, REQUIRES_SIGNING_FLAG, False):
            name = getattr(guard, "__name__", repr(guard))
            raise StartupError(
                f"{name} guards {where} but url_signing is not configured; "
                "pass url_signing=UrlSigningConfig(secret=...) to LaurenFactory.create",
                detail={"reason": "url_signing_disabled", "guard": name, "where": where},
            )


def _own_name(cls: Any, attr: str) -> str | None:
    meta = cls.__dict__.get(attr) if cls is not None else None
    return getattr(meta, "name", None)


def _join(*parts: str | None) -> str:
    return ".".join(part for part in parts if part)


def build_route_table(
    compiled_handlers: Mapping[tuple[str, str], Any],
    ws_gateways: Mapping[str, Any],
    builder: UrlBuilder,
    *,
    logger: Any,
    http_entries: Iterable[Any] = (),
    ws_entries: Iterable[Any] = (),
) -> RouteTable:
    """End of Phase 5: name every route and gateway, check uniqueness, bind the builder."""
    from ..decorators import CONTROLLER_META, MODULE_META
    from ..websockets import WS_CONTROLLER_META

    http_entry_map = {entry.handler_key: entry for entry in http_entries}
    ws_entry_map = {entry.handler_key[1]: entry for entry in ws_entries}
    handlers = [
        ch for ch in compiled_handlers.values() if not getattr(ch.handler_fn, "__lauren_docs_stub__", False)
    ]
    counts = Counter((ch.controller_cls, ch.handler_fn) for ch in handlers)
    http: list[RouteSpec] = []
    for ch in handlers:
        cls, fn = ch.controller_cls, ch.handler_fn
        local = getattr(ch.route_meta, "name", None)
        explicit = (
            _join(_own_name(ch.owning_module, MODULE_META), _own_name(cls, CONTROLLER_META), local)
            if local is not None
            else None
        )
        derived = None
        if local is None:
            if counts[(cls, fn)] == 1:
                derived = (
                    fn.__name__ if cls.__dict__.get(APP_ROUTES_FLAG) else f"{cls.__name__}.{fn.__name__}"
                )
            else:
                logger.verbose(
                    f"{cls.__name__}.{fn.__name__} handles {counts[(cls, fn)]} routes without name=; "
                    f"{ch.route_meta.method} {ch.path_template} has no route name",
                    context="RouteNames",
                )
        entry = http_entry_map.get((ch.route_meta.method, (ch.host or "") + ch.path_template))
        http.append(
            RouteSpec(
                ch.route_meta.method,
                ch.path_template,
                fn,
                cls,
                explicit,
                local,
                derived,
                segments=entry._segments if entry is not None else None,
                host=ch.host,
                host_pattern=entry._host_pattern if entry is not None else None,
            )
        )
    ws: list[RouteSpec] = []
    for path_template, gateway in ws_gateways.items():
        cls = gateway.controller_cls
        local = _own_name(cls, WS_CONTROLLER_META)
        explicit = _join(_own_name(gateway.owning_module, MODULE_META), local) if local is not None else None
        derived = cls.__name__ if local is None else None
        entry = ws_entry_map.get(path_template)
        ws.append(
            RouteSpec(
                "WEBSOCKET",
                gateway.path_template,
                cls,
                cls,
                explicit,
                local,
                derived,
                segments=entry._segments if entry is not None else None,
                host=gateway.host,
                host_pattern=entry._host_pattern if entry is not None else None,
            )
        )
    table = RouteTable(http, ws, log=lambda message: logger.verbose(message, context="RouteNames"))
    builder._bind(table)
    return table


__all__ = [
    "APP_ROUTES_FLAG",
    "REQUIRES_SIGNING_FLAG",
    "UrlSetup",
    "build_route_table",
    "check_signature_guards",
    "install_urls",
    "prepare_urls",
]

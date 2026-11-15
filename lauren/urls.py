"""Named routes, URL generation, trusted proxies and signed URLs.

Every HTTP route has a name — ``name=`` on the verb decorator (namespaced by
``@controller(name=...)`` / ``@module(name=...)``) or the derived
``ControllerClass.method`` — and every ``@ws_controller`` gateway too. URLs
are built from a name or a **handler reference** (``UserController.show``,
checked by Python and type checkers):

* ``request.url_path_for(target, **params)`` — path with the request's root
  path (mounts, ``root_path=``, a trusted ``X-Forwarded-Prefix``);
* ``request.url_for(target, **params)`` — absolute, on the request's scheme
  and host;
* ``request.ws_url_for(gateway, **params)`` — ``ws://`` / ``wss://``;
* ``app.url_path_for(target, **params)`` — app-relative path;
* :class:`UrlBuilder` — injectable, for code without a request; absolute URLs
  use ``public_base_url=``.

``ProxyConfig`` makes ``request.client`` / ``scheme`` / ``host`` /
``root_path`` honour ``Forwarded`` / ``X-Forwarded-*`` from trusted proxies
only. ``UrlSigningConfig`` + :meth:`UrlBuilder.signed_url_for` +
:class:`ValidSignature` give expiring, tamper-proof links.

Example::

    from lauren import LaurenFactory, controller, get, module, post
    from lauren.responses import Created
    from lauren.types import Request

    @controller("/users", name="users")
    class UserController:
        @get("/{id}", name="show")
        async def show(self, id: int) -> dict:
            return {"id": id}

        @post("/")
        async def create(self, request: Request) -> Created[dict]:
            return Created({"id": 7}, location=request.url_path_for(UserController.show, id=7))
"""

from __future__ import annotations

from typing import Any

from .types import Scope
from ._urls._forwarded import ProxyConfig
from ._urls._module import REQUIRES_SIGNING_FLAG
from ._urls._registry import UrlBuilder
from ._urls._signing import UrlSigningConfig
from ._urls._url import URL
from .decorators import injectable
from .exceptions import ForbiddenError


#: Close code a WebSocket connection gets from :class:`ValidSignature`.
WS_CLOSE_CODE = 4403


@injectable(scope=Scope.SINGLETON)
class ValidSignature:
    """Guard: let a request through only when its URL carries a valid, unexpired signature.

    Use it with ``@use_guards(ValidSignature)`` on routes (or gateways) whose
    links come from :meth:`UrlBuilder.signed_url_for`. A missing, tampered or
    expired signature raises :class:`~lauren.exceptions.ForbiddenError` (403)
    with ``detail["reason"]`` ``"signature_missing"``,
    ``"signature_invalid"`` or ``"signature_expired"``; a WebSocket
    connection is closed with code 4403 and that reason. Using it without
    ``url_signing=`` fails ``LaurenFactory.create``
    (``reason="url_signing_disabled"``).
    """

    def __init__(self, urls: UrlBuilder) -> None:
        self._urls = urls

    async def can_activate(self, context: Any) -> bool:
        reason = self._urls._verify(context.request)
        if reason is None:
            return True
        connection = getattr(context, "connection", None)
        if connection is not None:
            # A WebSocket: refuse the connection with a policy close code.
            await connection.close(code=WS_CLOSE_CODE, reason=reason)
            return False
        raise ForbiddenError("the URL signature is missing, invalid or expired", detail={"reason": reason})


setattr(ValidSignature, REQUIRES_SIGNING_FLAG, True)


__all__ = ["ProxyConfig", "URL", "UrlBuilder", "UrlSigningConfig", "ValidSignature"]

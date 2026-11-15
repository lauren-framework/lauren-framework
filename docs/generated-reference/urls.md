# URLs, Proxies & Signed Links

Named-route URLs and trusted public origins.

### `URL`

An immutable URL: a ``str`` with parsed components.

Usable anywhere a string is — ``Response.redirect(url)``, a header value,
a JSON field. Built by the ``url_for`` family; ``URL("...")`` wraps any
string. ``host`` keeps IPv6 brackets; ``port`` is ``None`` unless the URL
names one.

### `UrlBuilder`

Builds URLs to this app's routes — in services, jobs and background tasks.

One per app, injectable everywhere (``def __init__(self, urls: UrlBuilder)``).
Paths include the app's public root path (the path of ``public_base_url=``,
else ``root_path=``); absolute URLs use ``public_base_url=`` — a request's
``Host`` header is never consulted here. Inside a request handler prefer
``request.url_for`` / ``request.url_path_for``, which know the request's
own root path, scheme and host.

``target`` is a route name (``"users.show"``) or a handler reference
(``UserController.show``); ``route=`` picks one route of a handler that
has several. Unknown targets raise :class:`LookupError`, wrong path
parameters :class:`ValueError`.

### `ProxyConfig`

Which reverse proxies Lauren trusts, and which forwarding headers it reads.

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

### `UrlSigningConfig`

Signed-URL settings: ``LaurenFactory.create(..., url_signing=UrlSigningConfig(...))``.

* ``secret`` — str / bytes / :class:`~lauren.config.Secret`, or a
  sequence of them: the first signs, all verify (rotate by prepending a
  new key). Never shown in ``repr``.
* ``default_ttl`` — seconds a link stays valid when ``expires_in`` is not
  given.
* ``bind_method`` — also sign the HTTP method (a link for ``GET`` does not
  authorise ``DELETE`` on the same path).
* ``bind_host`` — also sign scheme and host (needs ``public_base_url=``).
* ``expires_param`` / ``signature_param`` — query parameter names.
* ``clock`` — returns the current unix time (tests pass a fake one).

Signed means tamper-proof, not secret: the URL, including every query
parameter, is readable by whoever holds it.

### `ValidSignature`

```python
class ValidSignature(urls: UrlBuilder)
```

Guard: let a request through only when its URL carries a valid, unexpired signature.

Use it with ``@use_guards(ValidSignature)`` on routes (or gateways) whose
links come from :meth:`UrlBuilder.signed_url_for`. A missing, tampered or
expired signature raises :class:`~lauren.exceptions.ForbiddenError` (403)
with ``detail["reason"]`` ``"signature_missing"``,
``"signature_invalid"`` or ``"signature_expired"``; a WebSocket
connection is closed with code 4403 and that reason. Using it without
``url_signing=`` fails ``LaurenFactory.create``
(``reason="url_signing_disabled"``).

#### `ValidSignature.can_activate`

```python
def can_activate(self, context: Any) -> bool
```

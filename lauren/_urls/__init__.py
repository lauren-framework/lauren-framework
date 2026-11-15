"""Named routes, URL generation, trusted proxies and signed URLs (private).

The public surface lives in :mod:`lauren.urls`. Modules here:

* ``_url`` — the :class:`URL` value type, authority parsing and the
  ``url_for`` accessors shared by ``Request``, ``WebSocket`` and
  ``WsUpgradeRequest`` (stdlib only, so :mod:`lauren.types` can import it);
* ``_names`` — route-name syntax, validated when a decorator runs;
* ``_template`` — URL templates compiled once per route at startup, and the
  path-parameter formatting hook;
* ``_registry`` — the per-app name registry and :class:`UrlBuilder`;
* ``_forwarded`` — :class:`ProxyConfig` and the forwarding-header resolver;
* ``_signing`` — :class:`UrlSigningConfig`, the URL signer and
  :class:`ValidSignature`;
* ``_module`` — the ``LaurenFactory.create`` hooks.

This ``__init__`` imports nothing so :mod:`lauren.types` can depend on
``_url`` without an import cycle.
"""

"""Private session-management runtime.

Public symbols are re-exported through ``lauren.sessions`` (and the
top-level ``lauren`` namespace). This package holds the implementation:
the :class:`Session` object, the pluggable stores, HMAC signing, the
serialiser, the :class:`SessionConfig` + validation, and the
``_SessionEngine`` the factory installs.

The only import the framework runtime needs is :class:`Session` (for
native ``session: Session`` handler injection) and
:func:`configure_sessions` (the factory wiring helper). Nothing here
imports ``lauren._asgi``, so the engine can be referenced from the ASGI
layer without a cycle.
"""

from __future__ import annotations

from typing import Any

from ._config import ResolvedSessionConfig, SessionConfig, resolve_session_config
from ._engine import _SessionEngine
from ._revocation import InMemoryRevocationStore, RevocationStore
from ._serializer import JSONSessionSerializer, SessionSerializer
from ._session import Session
from ._store import InMemorySessionStore, SessionStore, SignedCookieSessionStore

__all__ = [
    "Session",
    "SessionConfig",
    "SessionStore",
    "InMemorySessionStore",
    "SignedCookieSessionStore",
    "SessionSerializer",
    "JSONSessionSerializer",
    "RevocationStore",
    "InMemoryRevocationStore",
    # internal wiring (not part of the public lauren surface)
    "configure_sessions",
    "resolve_session_config",
    "ResolvedSessionConfig",
]


def configure_sessions(config: SessionConfig, container: Any) -> type:
    """Validate ``config``, build the engine, register DI providers.

    Returns the ``_SessionEngine`` class token. The caller
    (``LaurenFactory.create``) prepends it to the global-middleware list
    so the engine runs outermost (before routing), and the DI container
    resolves the token to the single engine instance registered here.

    Also binds the resolved store under the :class:`SessionStore` token
    so application services may inject ``store: SessionStore`` directly
    (e.g. an admin endpoint that revokes sessions).
    """
    from .._di.custom import use_value

    resolved = resolve_session_config(config)
    engine = _SessionEngine(resolved)

    existing = {p.cls for p in container.all_providers()}
    container.register_custom(use_value(provide=_SessionEngine, value=engine), owning_module=None)
    if SessionStore not in existing:
        container.register_custom(use_value(provide=SessionStore, value=resolved.store), owning_module=None)
    # Expose the revocation store for injection so an app can offer a
    # "log out everywhere" endpoint (revoke_user) or inspect the deny-list.
    if resolved.revocation is not None and RevocationStore not in existing:
        container.register_custom(
            use_value(provide=RevocationStore, value=resolved.revocation), owning_module=None
        )
    return _SessionEngine

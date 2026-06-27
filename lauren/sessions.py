"""First-class session management for lauren.

Enable sessions with a single factory kwarg::

    from lauren import LaurenFactory, SessionConfig

    app = LaurenFactory.create(
        AppModule,
        sessions=SessionConfig(secret="a-long-random-secret"),
    )

Then a handler receives a mutable, dict-like session at zero per-request
reflection cost (the same mechanism as ``ExecutionContext`` injection)::

    from lauren import Session, controller, get, post

    @controller("/account")
    class AccountController:
        @get("/visits")
        async def visits(self, session: Session) -> dict:
            session["visits"] = session.get("visits", 0) + 1
            return {"visits": session["visits"]}

        @post("/login")
        async def login(self, session: Session) -> dict:
            session.regenerate_id()       # session-fixation defence
            session["user_id"] = "u-42"
            return {"ok": True}

        @post("/logout")
        async def logout(self, session: Session) -> dict:
            session.invalidate()          # drop server row + expire cookie
            return {"ok": True}

The cookie is HMAC-signed (never forgeable without the secret), and the
defaults are secure (``HttpOnly``, ``Secure``, ``SameSite=Lax``). Unsafe
or contradictory configuration is rejected inside
``LaurenFactory.create`` — never at runtime.

Two stores ship in core: :class:`InMemorySessionStore` (dev /
single-worker) and the stateless :class:`SignedCookieSessionStore` (the
whole payload rides in the signed cookie). Production multi-worker
deployments implement the :class:`SessionStore` Protocol over Redis or a
database. ``request.state.session`` is the non-injected equivalent of the
``session: Session`` parameter for middleware/guards/interceptors that
hold only a ``Request``.

This is a general-purpose session mechanism (anonymous or authenticated,
any JSON-compatible payload). For authentication *gating* — verifying a
session and populating ``request.state.user`` — see the companion
``lauren-guards`` package's ``session_cookie`` guard, which composes on
top of a session id.
"""

from __future__ import annotations

from .exceptions import SessionConfigError
from ._sessions import (
    InMemorySessionStore,
    JSONSessionSerializer,
    Session,
    SessionConfig,
    SessionSerializer,
    SessionStore,
    SignedCookieSessionStore,
)

__all__ = [
    "Session",
    "SessionConfig",
    "SessionStore",
    "InMemorySessionStore",
    "SignedCookieSessionStore",
    "SessionSerializer",
    "JSONSessionSerializer",
    "SessionConfigError",
]

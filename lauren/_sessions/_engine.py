"""The session engine — load on the way in, persist on the way out.

The engine is installed by ``LaurenFactory.create`` as the **outermost**
global middleware when ``sessions=`` is set. It is the only component
that touches both the request (read the cookie, build a ``Session``,
attach it to ``request.state.session``) and the response (persist the
session and set/refresh/expire the ``Set-Cookie`` header). It is wired
directly by the factory — never registered by users — so its ordering is
deterministic.

Persistence is dirty-tracked: a session is written back and the cookie
re-emitted only when it is new-with-content, modified, regenerated,
invalidated, or (under ``rolling``) refreshed. A pure read touches
neither the store nor the response.

When a :class:`RevocationStore` is configured, every cookie additionally
carries a unique token id (``j``) and an issued-at stamp (``t``):
``invalidate()`` / ``regenerate_id()`` deny-list the prior token, and a
per-user cutoff (``revoke_user``) rejects any session minted before it.
Without a revocation store the cookie format and behaviour are unchanged
(the cookie store stays truly stateless).
"""

from __future__ import annotations

import base64
import secrets
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from ..types import Request, Response
from ._config import ResolvedSessionConfig
from ._session import Session


def _now() -> float:
    return time.time()


def _token() -> str:
    return secrets.token_urlsafe(16)


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


@dataclass(slots=True)
class _Inbound:
    """Per-request load metadata threaded from load to persist."""

    original_id: str | None = None  # server-side row id (server store)
    original_jti: str | None = None  # cookie token id (revocation)
    original_iat: float | None = None  # issued-at stamp (revocation)
    loaded_expiry: float | None = None  # the inbound cookie's expiry


class _SessionEngine:
    """Middleware-shaped session lifecycle engine (see module docstring)."""

    __slots__ = (
        "store",
        "signer",
        "serializer",
        "cookie_name",
        "max_age",
        "idle_timeout",
        "rolling",
        "path",
        "domain",
        "secure",
        "http_only",
        "same_site",
        "autoload",
        "client_side",
        "max_cookie_bytes",
        "revocation",
        "user_id_key",
    )

    def __init__(self, resolved: ResolvedSessionConfig) -> None:
        self.store = resolved.store
        self.signer = resolved.signer
        self.serializer = resolved.serializer
        self.cookie_name = resolved.cookie_name
        self.max_age = resolved.max_age
        self.idle_timeout = resolved.idle_timeout
        self.rolling = resolved.rolling
        self.path = resolved.path
        self.domain = resolved.domain
        self.secure = resolved.secure
        self.http_only = resolved.http_only
        self.same_site = resolved.same_site
        self.autoload = resolved.autoload
        self.client_side = resolved.client_side
        self.max_cookie_bytes = resolved.max_cookie_bytes
        self.revocation = resolved.revocation
        self.user_id_key = resolved.user_id_key

    # -- middleware entry point ---------------------------------------

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        session, inbound = await self._load(request)
        request.state.session = session
        response = await call_next(request)
        return await self._persist(response, session, inbound)

    # -- load ----------------------------------------------------------

    async def _load(self, request: Request) -> tuple[Session, _Inbound]:
        inbound = _Inbound()
        data: dict[str, Any] = {}
        is_new = True

        cookie_val = request.cookies.get(self.cookie_name)
        if cookie_val:
            unsigned = self.signer.unsign(cookie_val)
            if unsigned is not None:
                if self.revocation is None:
                    data, is_new = await self._load_plain(unsigned, inbound)
                else:
                    data, is_new = await self._load_revocable(unsigned, inbound)

        session = Session(
            data=data,
            id=inbound.original_id or "",
            is_new=is_new,
            new_id_factory=self.store.new_id,
        )
        return session, inbound

    async def _load_plain(self, unsigned: str, inbound: _Inbound) -> tuple[dict[str, Any], bool]:
        if self.client_side:
            env = self._decode_envelope(unsigned)
            if env is not None:
                expiry = env.get("x")
                if expiry is None or _now() < expiry:
                    payload = env.get("d")
                    if isinstance(payload, dict):
                        inbound.loaded_expiry = expiry
                        return payload, False
        else:
            loaded = await self.store.load(unsigned)
            if loaded is not None:
                inbound.original_id = unsigned
                return loaded, False
        return {}, True

    async def _load_revocable(self, unsigned: str, inbound: _Inbound) -> tuple[dict[str, Any], bool]:
        assert self.revocation is not None
        env = self._decode_envelope(unsigned)
        if env is None:
            return {}, True
        expiry = env.get("x")
        if expiry is not None and _now() >= expiry:
            return {}, True

        jti = env.get("j")
        iat = env.get("t")
        candidate: dict[str, Any] | None = None

        if self.client_side:
            # Per-session deny-list — the cookie store's row-deletion analogue.
            if jti and await self.revocation.is_token_revoked(jti):
                return {}, True
            payload = env.get("d")
            candidate = payload if isinstance(payload, dict) else None
        else:
            sid = env.get("i")
            candidate = await self.store.load(sid) if sid else None
            if candidate is not None:
                inbound.original_id = sid

        if candidate is None:
            return {}, True

        # Per-user cutoff — "log out everywhere" / force-logout-on-change.
        uid = candidate.get(self.user_id_key)
        if uid and iat is not None:
            cutoff = await self.revocation.user_cutoff(str(uid))
            if cutoff is not None and iat < cutoff:
                return {}, True

        inbound.loaded_expiry = expiry
        inbound.original_jti = jti if isinstance(jti, str) else None
        inbound.original_iat = float(iat) if isinstance(iat, (int, float)) else None
        return candidate, False

    # -- persist -------------------------------------------------------

    async def _persist(self, response: Response, session: Session, inbound: _Inbound) -> Response:
        if self.revocation is None:
            return await self._persist_plain(response, session, inbound)
        return await self._persist_revocable(response, session, inbound)

    async def _persist_plain(self, response: Response, session: Session, inbound: _Inbound) -> Response:
        if session.is_invalidated:
            if not self.client_side and inbound.original_id:
                await self.store.delete(inbound.original_id)
            return self._expire_cookie(response)
        if self.client_side:
            return self._persist_cookie_store(response, session, inbound.loaded_expiry)
        return await self._persist_server_store(response, session, inbound.original_id)

    async def _persist_server_store(
        self, response: Response, session: Session, original_id: str | None
    ) -> Response:
        has_content = bool(session) or session.is_modified
        should_save = (session.is_modified or (self.rolling and not session.is_new)) and has_content

        emit_cookie = False
        sid = session.id or ""
        if should_save:
            if not sid:
                sid = self.store.new_id()
                session._id = sid
            if original_id and original_id != sid:
                await self.store.delete(original_id)
            await self.store.save(sid, session.as_dict(), max_age=(self.idle_timeout or self.max_age))
            if session.is_new or original_id != sid:
                emit_cookie = True
        if self.rolling and not session.is_new and sid and not emit_cookie:
            emit_cookie = True

        if emit_cookie and sid:
            return self._set_cookie(response, self.signer.sign(sid))
        return response

    def _persist_cookie_store(
        self, response: Response, session: Session, loaded_expiry: float | None
    ) -> Response:
        should_emit = session.is_modified or (self.rolling and not session.is_new)
        if not (should_emit and (bool(session) or self.rolling)):
            return response
        envelope: dict[str, Any] = {"d": session.as_dict()}
        expiry = self._compute_expiry(loaded_expiry)
        if expiry is not None:
            envelope["x"] = expiry
        return self._set_cookie(response, self._encode_and_sign(envelope, enforce_size=True))

    async def _persist_revocable(self, response: Response, session: Session, inbound: _Inbound) -> Response:
        assert self.revocation is not None
        remaining = self._revoke_ttl(inbound.loaded_expiry)

        if session.is_invalidated:
            if not self.client_side and inbound.original_id:
                await self.store.delete(inbound.original_id)
            if inbound.original_jti:
                await self.revocation.revoke_token(inbound.original_jti, ttl=remaining)
            return self._expire_cookie(response)

        # On id rotation, deny-list the prior cookie lineage so a replayed
        # pre-login cookie cannot be reused (cookie-store fixation defence).
        if session._regenerated and inbound.original_jti:
            await self.revocation.revoke_token(inbound.original_jti, ttl=remaining)

        iat = inbound.original_iat if inbound.original_iat is not None else _now()
        jti = inbound.original_jti
        if jti is None or session._regenerated:
            jti = _token()

        if self.client_side:
            should_emit = session.is_modified or (self.rolling and not session.is_new)
            if not (should_emit and (bool(session) or self.rolling)):
                return response
            envelope: dict[str, Any] = {"d": session.as_dict(), "t": iat, "j": jti}
            expiry = self._compute_expiry(inbound.loaded_expiry)
            if expiry is not None:
                envelope["x"] = expiry
            return self._set_cookie(response, self._encode_and_sign(envelope, enforce_size=True))

        # Server-side store: data in the store, metadata (id/iat) in the cookie.
        has_content = bool(session) or session.is_modified
        should_save = (session.is_modified or (self.rolling and not session.is_new)) and has_content
        emit_cookie = False
        sid = session.id or ""
        if should_save:
            if not sid:
                sid = self.store.new_id()
                session._id = sid
            if inbound.original_id and inbound.original_id != sid:
                await self.store.delete(inbound.original_id)
            await self.store.save(sid, session.as_dict(), max_age=(self.idle_timeout or self.max_age))
            if session.is_new or inbound.original_id != sid:
                emit_cookie = True
        if self.rolling and not session.is_new and sid and not emit_cookie:
            emit_cookie = True

        if emit_cookie and sid:
            envelope = {"i": sid, "t": iat}
            expiry = self._compute_expiry(inbound.loaded_expiry)
            if expiry is not None:
                envelope["x"] = expiry
            return self._set_cookie(response, self._encode_and_sign(envelope, enforce_size=False))
        return response

    # -- helpers -------------------------------------------------------

    def _compute_expiry(self, loaded_expiry: float | None) -> float | None:
        if self.rolling or self.idle_timeout:
            window = self.idle_timeout or self.max_age
            return (_now() + window) if window else None
        if loaded_expiry is not None:
            return loaded_expiry
        return (_now() + self.max_age) if self.max_age else None

    def _revoke_ttl(self, loaded_expiry: float | None) -> int | None:
        if loaded_expiry is not None:
            return max(1, int(loaded_expiry - _now()))
        # Config guarantees a finite window when revocation is enabled.
        return self.idle_timeout or self.max_age

    def _encode_and_sign(self, envelope: dict[str, Any], *, enforce_size: bool) -> str:
        signed = self.signer.sign(_b64encode(self.serializer.dumps(envelope)))
        if enforce_size and len(signed) > self.max_cookie_bytes:
            raise ValueError(
                f"session cookie payload is {len(signed)} bytes, exceeding the "
                f"{self.max_cookie_bytes}-byte limit; store less in the session "
                "or use a server-side store"
            )
        return signed

    def _set_cookie(self, response: Response, value: str) -> Response:
        return response.with_cookie(
            self.cookie_name,
            value,
            max_age=self.max_age,
            path=self.path,
            domain=self.domain,
            secure=self.secure,
            http_only=self.http_only,
            same_site=self.same_site,
        )

    def _expire_cookie(self, response: Response) -> Response:
        return response.with_cookie(
            self.cookie_name,
            "",
            max_age=0,
            path=self.path,
            domain=self.domain,
            secure=self.secure,
            http_only=self.http_only,
            same_site=self.same_site,
        )

    def _decode_envelope(self, encoded: str) -> dict[str, Any] | None:
        try:
            env = self.serializer.loads(_b64decode(encoded))
        except Exception:
            return None
        return env if isinstance(env, dict) else None

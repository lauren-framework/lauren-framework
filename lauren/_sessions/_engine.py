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
"""

from __future__ import annotations

import base64
import time
from typing import Any, Awaitable, Callable

from ..types import Request, Response
from ._config import ResolvedSessionConfig
from ._session import Session


def _now() -> float:
    return time.time()


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


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

    # -- middleware entry point ---------------------------------------

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        original_id: str | None = None
        loaded_expiry: float | None = None
        data: dict[str, Any] = {}
        is_new = True

        cookie_val = request.cookies.get(self.cookie_name)
        if cookie_val:
            unsigned = self.signer.unsign(cookie_val)
            if unsigned is not None:
                if self.client_side:
                    env = self._decode_envelope(unsigned)
                    if env is not None:
                        expiry = env.get("x")
                        if expiry is None or _now() < expiry:
                            payload = env.get("d")
                            if isinstance(payload, dict):
                                data = payload
                                loaded_expiry = expiry
                                is_new = False
                else:
                    loaded = await self.store.load(unsigned)
                    if loaded is not None:
                        original_id = unsigned
                        data = loaded
                        is_new = False

        session = Session(
            data=data,
            id=original_id or "",
            is_new=is_new,
            new_id_factory=self.store.new_id,
        )
        request.state.session = session

        response = await call_next(request)
        return await self._persist(response, session, original_id, loaded_expiry)

    # -- persistence ---------------------------------------------------

    async def _persist(
        self,
        response: Response,
        session: Session,
        original_id: str | None,
        loaded_expiry: float | None,
    ) -> Response:
        if session.is_invalidated:
            if not self.client_side and original_id:
                await self.store.delete(original_id)
            return self._expire_cookie(response)
        if self.client_side:
            return self._persist_cookie_store(response, session, loaded_expiry)
        return await self._persist_server_store(response, session, original_id)

    async def _persist_server_store(
        self,
        response: Response,
        session: Session,
        original_id: str | None,
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
                # Session id rotated (regenerate_id) — drop the old row.
                await self.store.delete(original_id)
            await self.store.save(sid, session.as_dict(), max_age=(self.idle_timeout or self.max_age))
            if session.is_new or original_id != sid:
                emit_cookie = True
        # Under rolling, refresh the cookie window even on an unmodified read.
        if self.rolling and not session.is_new and sid and not emit_cookie:
            emit_cookie = True

        if emit_cookie and sid:
            return self._set_cookie(response, self.signer.sign(sid))
        return response

    def _persist_cookie_store(
        self,
        response: Response,
        session: Session,
        loaded_expiry: float | None,
    ) -> Response:
        should_emit = session.is_modified or (self.rolling and not session.is_new)
        has_content = bool(session)
        if not (should_emit and (has_content or self.rolling)):
            return response

        if self.rolling or self.idle_timeout:
            window = self.idle_timeout or self.max_age
            expiry: float | None = (_now() + window) if window else None
        elif loaded_expiry is not None:
            expiry = loaded_expiry
        else:
            expiry = (_now() + self.max_age) if self.max_age else None

        envelope: dict[str, Any] = {"d": session.as_dict()}
        if expiry is not None:
            envelope["x"] = expiry
        encoded = _b64encode(self.serializer.dumps(envelope))
        signed = self.signer.sign(encoded)
        if len(signed) > self.max_cookie_bytes:
            raise ValueError(
                f"session cookie payload is {len(signed)} bytes, exceeding the "
                f"{self.max_cookie_bytes}-byte limit; store less in the session "
                "or use a server-side store"
            )
        return self._set_cookie(response, signed)

    # -- cookie helpers ------------------------------------------------

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

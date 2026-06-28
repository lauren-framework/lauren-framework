"""``SessionConfig`` and its startup validation.

The public, frozen :class:`SessionConfig` is the single declaration point
for sessions — passed as ``LaurenFactory.create(..., sessions=...)``.
:func:`resolve_session_config` validates it (raising
:class:`SessionConfigError` on any unsafe combination) and lowers it to
an internal :class:`ResolvedSessionConfig` the engine consumes.

All validation runs inside ``LaurenFactory.create`` so a misconfigured
session policy fails at startup, never at runtime.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Sequence

from ..exceptions import SessionConfigError
from ._revocation import RevocationStore
from ._serializer import JSONSessionSerializer, SessionSerializer
from ._signing import Signer, normalize_secrets
from ._store import InMemorySessionStore, SessionStore

_SAME_SITE_CANONICAL = {"lax": "Lax", "strict": "Strict", "none": "None"}


@dataclass(frozen=True, slots=True)
class SessionConfig:
    """Declarative session policy passed to ``LaurenFactory.create``.

    Secure by default: ``http_only`` and ``secure`` are on, the cookie is
    HMAC-signed (``secret`` is required), and ``same_site`` is ``"lax"``.
    Override per deployment, but the framework rejects unsafe combinations
    (e.g. ``same_site="none"`` without ``secure``) at startup.
    """

    secret: str | bytes | Sequence[str | bytes] | None = None
    store: SessionStore | None = None
    cookie_name: str = "lauren_session"
    max_age: int | None = 1_209_600  # 14 days
    idle_timeout: int | None = None
    rolling: bool = False
    path: str = "/"
    domain: str | None = None
    secure: bool = True
    http_only: bool = True
    same_site: str = "lax"
    serializer: SessionSerializer | None = None
    autoload: bool = True
    #: Opt-in revocation index. When set, every cookie carries a token id
    #: and an issued-at stamp; ``invalidate()`` deny-lists the token and
    #: ``RevocationStore.revoke_user(...)`` enables "log out everywhere".
    #: Leaving this ``None`` keeps the cookie store truly stateless.
    revocation_store: RevocationStore | None = None
    #: Session key holding the authenticated user id, consulted for the
    #: per-user revocation cutoff. Only used when ``revocation_store`` is set.
    user_id_key: str = "user_id"


@dataclass(slots=True)
class ResolvedSessionConfig:
    """Validated, lowered config the engine actually runs on."""

    store: SessionStore
    signer: Signer
    serializer: SessionSerializer
    cookie_name: str
    max_age: int | None
    idle_timeout: int | None
    rolling: bool
    path: str
    domain: str | None
    secure: bool
    http_only: bool
    same_site: str  # canonical: "Lax" | "Strict" | "None"
    autoload: bool
    client_side: bool
    max_cookie_bytes: int
    revocation: RevocationStore | None
    user_id_key: str
    raw: SessionConfig = field(repr=False, default=None)  # type: ignore[assignment]


def _fail(message: str, **detail: Any) -> SessionConfigError:
    return SessionConfigError(message, detail={"reason": "session_config", **detail})


def resolve_session_config(config: SessionConfig) -> ResolvedSessionConfig:
    """Validate ``config`` and lower it to a :class:`ResolvedSessionConfig`.

    Raises :class:`SessionConfigError` on any unsafe or contradictory
    setting. This is the sessions feature's startup choke point.
    """
    if not isinstance(config, SessionConfig):
        raise _fail(
            f"sessions= must be a SessionConfig instance; got {type(config).__name__}",
            got=type(config).__name__,
        )

    # --- same_site ---------------------------------------------------
    same_site_key = str(config.same_site).lower()
    if same_site_key not in _SAME_SITE_CANONICAL:
        raise _fail(
            f"same_site must be one of 'lax', 'strict', 'none'; got {config.same_site!r}",
            same_site=config.same_site,
        )
    same_site = _SAME_SITE_CANONICAL[same_site_key]

    if same_site == "None" and not config.secure:
        raise _fail(
            "same_site='none' requires secure=True (browsers reject "
            "SameSite=None cookies without the Secure attribute)",
            same_site=config.same_site,
            secure=config.secure,
        )

    # --- cookie-name prefixes ---------------------------------------
    name = config.cookie_name
    if not name:
        raise _fail("cookie_name must be a non-empty string")
    if name.startswith("__Host-"):
        if not config.secure or config.path != "/" or config.domain is not None:
            raise _fail(
                "a '__Host-' cookie requires secure=True, path='/', and domain=None",
                cookie_name=name,
                secure=config.secure,
                path=config.path,
                domain=config.domain,
            )
    elif name.startswith("__Secure-") and not config.secure:
        raise _fail(
            "a '__Secure-' cookie requires secure=True",
            cookie_name=name,
            secure=config.secure,
        )

    # --- lifetimes ---------------------------------------------------
    for label, value in (("max_age", config.max_age), ("idle_timeout", config.idle_timeout)):
        if value is not None and (not isinstance(value, int) or isinstance(value, bool) or value <= 0):
            raise _fail(f"{label} must be a positive integer or None; got {value!r}", **{label: value})

    # --- store + secret ---------------------------------------------
    store = config.store if config.store is not None else InMemorySessionStore()
    secrets = normalize_secrets(config.secret)
    requires_secret = bool(getattr(store, "requires_secret", True))
    if requires_secret and not secrets:
        raise _fail(
            f"store {type(store).__name__} requires a signing secret; pass "
            "SessionConfig(secret=...). Use a long random value and keep it "
            "out of source control.",
            store=type(store).__name__,
        )
    # Signing is always on, so a secret is always needed in practice.
    if not secrets:
        raise _fail("SessionConfig.secret is required (the cookie is always signed)")
    signer = Signer(secrets)

    serializer = config.serializer if config.serializer is not None else JSONSessionSerializer()
    client_side = bool(getattr(store, "client_side", False))
    max_cookie_bytes = int(getattr(store, "max_bytes", 4096))

    # --- revocation --------------------------------------------------
    if config.revocation_store is not None:
        if config.max_age is None and config.idle_timeout is None:
            raise _fail(
                "revocation requires a finite lifetime so deny-list entries can "
                "self-prune; set max_age or idle_timeout on SessionConfig",
            )
        if not config.user_id_key:
            raise _fail("user_id_key must be a non-empty string when revocation is enabled")

    return ResolvedSessionConfig(
        store=store,
        signer=signer,
        serializer=serializer,
        cookie_name=name,
        max_age=config.max_age,
        idle_timeout=config.idle_timeout,
        rolling=config.rolling,
        path=config.path,
        domain=config.domain,
        secure=config.secure,
        http_only=config.http_only,
        same_site=same_site,
        autoload=config.autoload,
        client_side=client_side,
        max_cookie_bytes=max_cookie_bytes,
        revocation=config.revocation_store,
        user_id_key=config.user_id_key,
        raw=config,
    )

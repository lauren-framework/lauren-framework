"""Signed URLs: ``UrlSigningConfig`` and the URL signer (private).

A signed URL carries two query parameters: ``exp`` (unix seconds) and
``sig``, the HMAC-SHA256 (hex) of a canonical message computed by the shared
:class:`lauren._signing.Signer` under the ``"url"`` namespace — key
rotation (the first secret signs, all verify) and constant-time comparison
come from there. The canonical message is::

    {METHOD}\\n{ORIGIN}\\n{app-relative path}\\n{query sorted, without exp/sig}\\n{exp}

``METHOD`` is empty unless ``bind_method=True`` and ``ORIGIN``
(``https://host[:port]``) is empty unless ``bind_host=True``. The path is the
app-relative path (without ``root_path`` / mount prefix), so a link stays
valid when the app is served under several prefixes. Path and query are
canonicalised from their decoded form, so equivalent encodings verify alike
and no other path can reuse a signature.
"""

from __future__ import annotations

import math
import re
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any
from urllib.parse import parse_qsl, quote, unquote

from .._signing import Signer, normalize_secrets
from ..exceptions import StartupError
from ._url import PATH_SAFE, WS_TO_HTTP, compose_origin, format_value

#: ``Signer`` namespace of signed URLs (a session or cursor signature never verifies here).
NAMESPACE = "url"

_HEX_SIG = re.compile(r"[0-9a-f]{64}")
_EXP = re.compile(r"[0-9]{1,12}")

#: ``detail["reason"]`` values of a rejected signed URL.
SIGNATURE_MISSING = "signature_missing"
SIGNATURE_INVALID = "signature_invalid"
SIGNATURE_EXPIRED = "signature_expired"


@dataclass(frozen=True, slots=True, kw_only=True)
class UrlSigningConfig:
    """Signed-URL settings: ``LaurenFactory.create(..., url_signing=UrlSigningConfig(...))``.

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
    """

    secret: Any = field(repr=False)
    default_ttl: float = 3600
    bind_method: bool = False
    bind_host: bool = False
    expires_param: str = "exp"
    signature_param: str = "sig"
    clock: Callable[[], float] | None = field(default=None, repr=False, compare=False)


def _invalid(field_name: str, value: Any, why: str) -> StartupError:
    shown = "<secret>" if field_name == "secret" else repr(value)
    return StartupError(
        f"UrlSigningConfig({field_name}={shown}) is invalid: {why}",
        detail={"reason": "invalid_url_signing_config", "field": field_name},
    )


def _seconds(value: Any) -> float | None:
    if isinstance(value, timedelta):
        value = value.total_seconds()
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value <= 0
    ):
        return None
    return float(value)


def _canonical_query(pairs: Iterable[tuple[str, str]]) -> str:
    return "&".join(f"{quote(key, safe='')}={quote(value, safe='')}" for key, value in sorted(pairs))


class UrlSigner:
    """A validated :class:`UrlSigningConfig`: signs query strings and verifies requests."""

    __slots__ = ("_signer", "default_ttl", "bind_method", "bind_host", "exp_param", "sig_param", "_clock")

    def __init__(self, config: Any, *, has_origin: bool) -> None:
        if not isinstance(config, UrlSigningConfig):
            raise StartupError(
                f"url_signing= expects a UrlSigningConfig, got {type(config).__name__}",
                detail={"reason": "invalid_url_signing_config", "got": type(config).__name__},
            )
        try:
            secrets = normalize_secrets(config.secret)
        except TypeError:
            raise _invalid("secret", None, "expected str / bytes / Secret or a sequence of them") from None
        if not secrets:
            raise _invalid("secret", None, "at least one non-empty secret is required")
        ttl = _seconds(config.default_ttl)
        if ttl is None:
            raise _invalid("default_ttl", config.default_ttl, "expected a positive number of seconds")
        for flag in ("bind_method", "bind_host"):
            if not isinstance(getattr(config, flag), bool):
                raise _invalid(flag, getattr(config, flag), "expected a bool")
        for name in ("expires_param", "signature_param"):
            value = getattr(config, name)
            if not isinstance(value, str) or not value or value != value.strip():
                raise _invalid(name, value, "expected a non-empty query parameter name")
        if config.expires_param == config.signature_param:
            raise _invalid("signature_param", config.signature_param, "must differ from expires_param")
        if config.clock is not None and not callable(config.clock):
            raise _invalid("clock", config.clock, "expected a callable returning unix seconds")
        if config.bind_host and not has_origin:
            raise _invalid("bind_host", True, "binding the host needs public_base_url= on the app")
        self._signer = Signer(secrets, namespace=NAMESPACE)
        self.default_ttl = ttl
        self.bind_method = config.bind_method
        self.bind_host = config.bind_host
        self.exp_param = config.expires_param
        self.sig_param = config.signature_param
        self._clock: Callable[[], float] = config.clock or time.time

    def _message(
        self, method: str, origin: str, path: str, pairs: Iterable[tuple[str, str]], exp: str
    ) -> str:
        return "\n".join(
            (
                method.upper() if self.bind_method else "",
                origin if self.bind_host else "",
                quote(path, safe=PATH_SAFE),
                _canonical_query(pairs),
                exp,
            )
        )

    def sign(
        self,
        *,
        method: str,
        origin: str,
        path: str,
        query: Sequence[tuple[str, Any]],
        expires_in: Any,
    ) -> str:
        """The query string (``query`` + ``exp`` + ``sig``) signing ``path`` (encoded, app-relative)."""
        ttl = self.default_ttl if expires_in is None else _seconds(expires_in)
        if ttl is None:
            raise ValueError(
                f"expires_in must be a positive number of seconds or a timedelta, got {expires_in!r}"
            )
        pairs: list[tuple[str, str]] = []
        for key, value in query:
            if key in (self.exp_param, self.sig_param):
                raise ValueError(f"query parameter {key!r} is reserved for the URL signature")
            values = value if isinstance(value, (list, tuple)) else (value,)
            pairs.extend((key, format_value(item)) for item in values if item is not None)
        exp = str(math.ceil(self._clock() + ttl))
        token = self._signer.sign(self._message(method, origin, unquote(path), pairs, exp))
        sig = token.rsplit(".", 1)[1]
        encoded = [f"{quote(key, safe='')}={quote(value, safe='')}" for key, value in pairs]
        encoded.append(f"{quote(self.exp_param, safe='')}={exp}")
        encoded.append(f"{quote(self.sig_param, safe='')}={sig}")
        return "&".join(encoded)

    def verify(self, *, method: str, origin: str, path: str, query: str) -> str | None:
        """``None`` when the request URL is validly signed, else the ``detail["reason"]``.

        ``path`` is the decoded app-relative request path; ``query`` the raw
        query string.
        """
        pairs = parse_qsl(query, keep_blank_values=True)
        exps = [value for key, value in pairs if key == self.exp_param]
        sigs = [value for key, value in pairs if key == self.sig_param]
        if not exps or not sigs:
            return SIGNATURE_MISSING
        if len(exps) != 1 or len(sigs) != 1 or not _EXP.fullmatch(exps[0]) or not _HEX_SIG.fullmatch(sigs[0]):
            return SIGNATURE_INVALID
        rest = [(key, value) for key, value in pairs if key not in (self.exp_param, self.sig_param)]
        message = self._message(method, origin, path, rest, exps[0])
        if self._signer.unsign(f"{message}.{sigs[0]}") is None:
            return SIGNATURE_INVALID
        if self._clock() > int(exps[0]):
            return SIGNATURE_EXPIRED
        return None


def request_origin(request: Any) -> str:
    """The canonical (``http``/``https``) origin of a request or upgrade request."""
    scheme = request.scheme
    return compose_origin(WS_TO_HTTP.get(scheme, scheme), request.host, request.port)


__all__ = [
    "NAMESPACE",
    "SIGNATURE_EXPIRED",
    "SIGNATURE_INVALID",
    "SIGNATURE_MISSING",
    "UrlSigner",
    "UrlSigningConfig",
    "request_origin",
]

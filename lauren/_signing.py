"""HMAC-SHA256 signing with key rotation, shared by every signed token (private).

One signer serves every feature that hands the client a value it must not
forge: the session cookie (:mod:`lauren._sessions`), pagination cursors
(:mod:`lauren._pagination`) and, later, signed URLs. Values are **signed**,
never encrypted — the client can read them but cannot alter them without the
server-side secret.

* A token is ``"<value>.<hexsig>"``: the hex HMAC-SHA256 of the value.
* Several secrets support rotation: the *first* secret signs, *all* secrets
  verify. Rotate by prepending a new secret and dropping the oldest once every
  token signed with it has expired.
* Verification is constant-time (``hmac.compare_digest`` over bytes, so
  adversarial non-ASCII input never raises).
* ``namespace`` separates purposes: a token signed for one namespace never
  verifies in another, even when an application reuses one secret for two
  features. The empty namespace (the session cookie's) signs the bare value,
  so existing session cookies stay valid.

This module imports nothing from lauren except the ``Secret`` helper, so any
subsystem can depend on it without an import cycle.
"""

from __future__ import annotations

import hmac
from hashlib import sha256
from typing import Any

from ._config._secret import is_secret

# Separator between the value and its signature. base64url payloads and
# ``secrets.token_urlsafe`` ids never contain ``.`` so a right-split is
# unambiguous.
_SEP = "."

#: Separator between a non-empty namespace and the signed value inside the MAC
#: input (never part of the token itself).
_NAMESPACE_SEP = b"\x00"


def normalize_secrets(
    secret: Any,
) -> tuple[bytes, ...]:
    """Coerce a user secret (str / bytes / ``Secret`` / sequence of those) to bytes.

    :class:`lauren.config.Secret` (and pydantic ``SecretStr`` / ``SecretBytes``)
    values are unwrapped here, so ``SessionConfig(secret=settings.session_secret)``
    works without the caller ever reading the secret. Empty entries are dropped.
    Returns an empty tuple when no usable secret was given so callers can raise
    a configuration error with a precise message.
    """
    if secret is None:
        return ()
    if isinstance(secret, (str, bytes)) or is_secret(secret):
        items: list[Any] = [secret]
    else:
        items = list(secret)
    out: list[bytes] = []
    for item in items:
        if is_secret(item):
            item = item.get_secret_value()
        if isinstance(item, str):
            if item:
                out.append(item.encode("utf-8"))
        elif isinstance(item, bytes):
            if item:
                out.append(item)
    return tuple(out)


class Signer:
    """Signs with the newest secret, verifies against every secret."""

    __slots__ = ("_secrets", "_prefix")

    def __init__(self, secrets: tuple[bytes, ...], *, namespace: str = "") -> None:
        if not secrets:
            raise ValueError("Signer requires at least one secret")
        self._secrets = secrets
        self._prefix = namespace.encode("utf-8") + _NAMESPACE_SEP if namespace else b""

    def _mac(self, value: str, key: bytes) -> bytes:
        # ``surrogatepass`` keeps signing total for any ``str`` (lone
        # surrogates included); the hexdigest itself is always ASCII bytes.
        message = self._prefix + value.encode("utf-8", "surrogatepass")
        return hmac.new(key, message, sha256).hexdigest().encode("ascii")

    def sign(self, value: str) -> str:
        """Return ``"<value>.<hexsig>"`` keyed on the newest secret."""
        return f"{value}{_SEP}{self._mac(value, self._secrets[0]).decode('ascii')}"

    def unsign(self, token: str) -> str | None:
        """Return the original value if any secret validates the signature.

        Returns ``None`` on a missing separator or a bad signature. The
        comparison is constant-time (over bytes, so adversarial non-ASCII
        input never raises) to avoid leaking the signature byte by byte.
        """
        parts = token.rsplit(_SEP, 1)
        if len(parts) != 2:
            return None
        value, sig = parts
        sig_bytes = sig.encode("utf-8", "surrogatepass")
        for key in self._secrets:
            if hmac.compare_digest(sig_bytes, self._mac(value, key)):
                return value
        return None


__all__ = ["Signer", "normalize_secrets"]

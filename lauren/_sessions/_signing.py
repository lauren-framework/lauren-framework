"""HMAC cookie signing with key rotation.

The session cookie carries either an opaque session id (server-side
stores) or a serialised payload (the stateless cookie store). In both
cases the value is **signed** — never encrypted — so a client cannot
forge it without the server-side secret. The signature is a hex
HMAC-SHA256 keyed on the secret; verification is constant-time.

Multiple secrets are supported for rotation: the *first* secret signs,
*all* secrets verify. Rotate by prepending a new secret and dropping the
oldest once every active cookie has aged out.
"""

from __future__ import annotations

import hmac
from hashlib import sha256
from typing import Sequence

# Separator between the value and its signature. base64url ids/payloads
# and ``secrets.token_urlsafe`` never contain ``.`` so a right-split is
# unambiguous.
_SEP = "."


def normalize_secrets(
    secret: str | bytes | Sequence[str | bytes] | None,
) -> tuple[bytes, ...]:
    """Coerce a user secret (str / bytes / sequence of either) to bytes.

    Returns an empty tuple when no usable secret was given so callers can
    raise a configuration error with a precise message.
    """
    if secret is None:
        return ()
    if isinstance(secret, (str, bytes)):
        items: list[str | bytes] = [secret]
    else:
        items = list(secret)
    out: list[bytes] = []
    for item in items:
        if isinstance(item, str):
            if item:
                out.append(item.encode("utf-8"))
        elif isinstance(item, bytes):
            if item:
                out.append(item)
    return tuple(out)


class Signer:
    """Signs with the newest secret, verifies against every secret."""

    __slots__ = ("_secrets",)

    def __init__(self, secrets: tuple[bytes, ...]) -> None:
        if not secrets:
            raise ValueError("Signer requires at least one secret")
        self._secrets = secrets

    def _mac(self, value: str, key: bytes) -> bytes:
        # ``surrogatepass`` keeps signing total for any ``str`` (lone
        # surrogates included); the hexdigest itself is always ASCII bytes.
        return hmac.new(key, value.encode("utf-8", "surrogatepass"), sha256).hexdigest().encode("ascii")

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

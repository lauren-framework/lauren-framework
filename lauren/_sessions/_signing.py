"""HMAC cookie signing with key rotation — the shared signer, seen from sessions.

The session cookie carries either an opaque session id (server-side stores) or
a serialised payload (the stateless cookie store). In both cases the value is
**signed** — never encrypted — so a client cannot forge it without the
server-side secret. Multiple secrets support rotation: the *first* secret
signs, *all* secrets verify.

The implementation lives in :mod:`lauren._signing` so pagination cursors (and
later signed URLs) share it; sessions sign in the empty namespace, so cookies
are byte-for-byte what they always were.
"""

from __future__ import annotations

from .._signing import _SEP, Signer, normalize_secrets

__all__ = ["Signer", "normalize_secrets", "_SEP"]

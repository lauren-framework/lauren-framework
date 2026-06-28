"""Unit tests for the revocation store (no ASGI app)."""

from __future__ import annotations

import time

from lauren import InMemoryRevocationStore, RevocationStore
from lauren._sessions._config import resolve_session_config
from lauren.sessions import SessionConfig


class TestTokenDenyList:
    async def test_revoke_and_check(self):
        store = InMemoryRevocationStore()
        assert await store.is_token_revoked("t1") is False
        await store.revoke_token("t1")
        assert await store.is_token_revoked("t1") is True

    async def test_unrevoked_token_is_false(self):
        store = InMemoryRevocationStore()
        await store.revoke_token("t1")
        assert await store.is_token_revoked("other") is False

    async def test_ttl_expiry_prunes(self):
        store = InMemoryRevocationStore()
        await store.revoke_token("t1", ttl=1)
        assert await store.is_token_revoked("t1") is True
        # Force the entry into the past; the next check prunes and reports False.
        store._tokens["t1"] = time.time() - 1
        assert await store.is_token_revoked("t1") is False
        assert store._token_count() == 0


class TestUserCutoff:
    async def test_no_cutoff_by_default(self):
        store = InMemoryRevocationStore()
        assert await store.user_cutoff("u1") is None

    async def test_revoke_user_sets_cutoff(self):
        store = InMemoryRevocationStore()
        await store.revoke_user("u1", cutoff=1000.0)
        assert await store.user_cutoff("u1") == 1000.0

    async def test_revoke_user_defaults_cutoff_to_now(self):
        store = InMemoryRevocationStore()
        before = time.time()
        await store.revoke_user("u1")
        cutoff = await store.user_cutoff("u1")
        assert cutoff is not None and cutoff >= before

    async def test_user_cutoff_ttl_prunes(self):
        store = InMemoryRevocationStore()
        await store.revoke_user("u1", cutoff=1000.0, ttl=1)
        store._users["u1"] = (1000.0, time.time() - 1)
        assert await store.user_cutoff("u1") is None


class TestRevocationStoreProtocol:
    def test_inmemory_satisfies_protocol(self):
        assert isinstance(InMemoryRevocationStore(), RevocationStore)


class TestRevocationConfigValidation:
    def test_revocation_requires_finite_lifetime(self):
        from lauren.exceptions import SessionConfigError

        try:
            resolve_session_config(
                SessionConfig(
                    secret="x" * 32,
                    revocation_store=InMemoryRevocationStore(),
                    max_age=None,
                    idle_timeout=None,
                )
            )
            raise AssertionError("expected SessionConfigError")
        except SessionConfigError:
            pass

    def test_revocation_with_idle_timeout_is_allowed(self):
        resolved = resolve_session_config(
            SessionConfig(
                secret="x" * 32,
                revocation_store=InMemoryRevocationStore(),
                max_age=None,
                idle_timeout=900,
            )
        )
        assert resolved.revocation is not None
        assert resolved.user_id_key == "user_id"

    def test_revocation_off_by_default(self):
        resolved = resolve_session_config(SessionConfig(secret="x" * 32))
        assert resolved.revocation is None

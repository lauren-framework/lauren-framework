"""Integration tests for field-level data encryption & key rotation (Skill 44)."""

from __future__ import annotations

import asyncio

import pytest
from cryptography.fernet import Fernet, InvalidToken

from lauren import LaurenFactory, Path, Scope, controller, get, injectable, module
from lauren.testing import TestClient


# ---------------------------------------------------------------------------
# FieldEncryptor
# ---------------------------------------------------------------------------


@injectable(scope=Scope.SINGLETON)
class FieldEncryptor:
    def __init__(self, primary_key: bytes | None = None) -> None:
        self._primary_key: bytes = primary_key or Fernet.generate_key()
        self._fernet: Fernet = Fernet(self._primary_key)
        self._old_keys: list[bytes] = []

    def encrypt(self, value: str) -> str:
        return self._fernet.encrypt(value.encode()).decode()

    def decrypt(self, token: str) -> str:
        from cryptography.fernet import MultiFernet

        if self._old_keys:
            multi = MultiFernet([Fernet(k) for k in [self._primary_key] + self._old_keys])
            return multi.decrypt(token.encode()).decode()
        return self._fernet.decrypt(token.encode()).decode()

    def rotate_key(self) -> bytes:
        self._old_keys.insert(0, self._primary_key)
        self._primary_key = Fernet.generate_key()
        self._fernet = Fernet(self._primary_key)
        return self._primary_key

    def re_encrypt(self, token: str) -> str:
        plaintext = self.decrypt(token)
        return self._fernet.encrypt(plaintext.encode()).decode()

    @staticmethod
    def generate_key() -> bytes:
        return Fernet.generate_key()


# ---------------------------------------------------------------------------
# Simple domain service using encryption
# ---------------------------------------------------------------------------


@injectable(scope=Scope.SINGLETON)
class VaultService:
    def __init__(self, encryptor: FieldEncryptor) -> None:
        self._encryptor = encryptor
        self._secrets: dict[str, str] = {}

    def store(self, name: str, secret: str) -> None:
        self._secrets[name] = self._encryptor.encrypt(secret)

    def retrieve(self, name: str) -> str | None:
        token = self._secrets.get(name)
        if token is None:
            return None
        return self._encryptor.decrypt(token)

    def rotate_and_reencrypt(self) -> bytes:
        new_key = self._encryptor.rotate_key()
        for name in list(self._secrets):
            self._secrets[name] = self._encryptor.re_encrypt(self._secrets[name])
        return new_key


@controller("/vault")
class VaultController:
    def __init__(self, vault: VaultService) -> None:
        self._vault = vault

    @get("/secret/{name}")
    async def get_secret(self, name: Path[str]) -> dict:
        value = self._vault.retrieve(name)
        if value is None:
            return {"found": False}
        return {"found": True, "value": value}


@module(controllers=[VaultController], providers=[FieldEncryptor, VaultService])
class VaultModule:
    pass


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestFieldEncryption:
    def test_encrypt_decrypt_roundtrip(self):
        enc = FieldEncryptor()
        token = enc.encrypt("my-secret")
        assert enc.decrypt(token) == "my-secret"

    def test_encrypt_produces_different_token_each_time(self):
        enc = FieldEncryptor()
        t1 = enc.encrypt("same")
        t2 = enc.encrypt("same")
        assert t1 != t2  # Fernet uses random IV

    def test_decrypt_with_wrong_key_raises(self):
        enc1 = FieldEncryptor()
        enc2 = FieldEncryptor()
        token = enc1.encrypt("secret")
        with pytest.raises(InvalidToken):
            enc2.decrypt(token)

    def test_rotate_key_returns_new_key(self):
        enc = FieldEncryptor()
        old_key = enc._primary_key
        new_key = enc.rotate_key()
        assert new_key != old_key

    def test_old_tokens_still_decrypt_after_rotation(self):
        enc = FieldEncryptor()
        token = enc.encrypt("secret-data")
        enc.rotate_key()
        assert enc.decrypt(token) == "secret-data"

    def test_multiple_rotations_old_tokens_still_work(self):
        enc = FieldEncryptor()
        token = enc.encrypt("persistent-secret")
        enc.rotate_key()
        enc.rotate_key()
        assert enc.decrypt(token) == "persistent-secret"

    def test_re_encrypt_produces_valid_token(self):
        enc = FieldEncryptor()
        old_token = enc.encrypt("re-encrypt-me")
        enc.rotate_key()
        new_token = enc.re_encrypt(old_token)
        assert enc.decrypt(new_token) == "re-encrypt-me"

    def test_re_encrypt_new_token_decryptable_without_old_keys(self):
        enc = FieldEncryptor()
        old_token = enc.encrypt("value")
        enc.rotate_key()
        new_token = enc.re_encrypt(old_token)
        # New token should be decryptable with a fresh encryptor using the new primary key
        fresh = FieldEncryptor(primary_key=enc._primary_key)
        assert fresh.decrypt(new_token) == "value"

    def test_generate_key_returns_bytes(self):
        key = FieldEncryptor.generate_key()
        assert isinstance(key, bytes)
        assert len(key) > 0

    def test_encrypt_unicode_string(self):
        enc = FieldEncryptor()
        value = "héllo wörld 中文"
        assert enc.decrypt(enc.encrypt(value)) == value

    def test_encrypt_empty_string(self):
        enc = FieldEncryptor()
        token = enc.encrypt("")
        assert enc.decrypt(token) == ""

    def test_vault_service_store_and_retrieve(self):
        enc = FieldEncryptor()
        vault = VaultService(enc)
        vault.store("api_key", "super-secret-key")
        assert vault.retrieve("api_key") == "super-secret-key"

    def test_vault_service_returns_none_for_missing(self):
        enc = FieldEncryptor()
        vault = VaultService(enc)
        assert vault.retrieve("nonexistent") is None

    def test_vault_service_rotate_and_reencrypt(self):
        enc = FieldEncryptor()
        vault = VaultService(enc)
        vault.store("key1", "value1")
        vault.store("key2", "value2")
        vault.rotate_and_reencrypt()
        assert vault.retrieve("key1") == "value1"
        assert vault.retrieve("key2") == "value2"

    def test_api_get_secret(self):
        app = LaurenFactory.create(VaultModule)
        vault_svc = asyncio.run(app.container.resolve(VaultService))
        vault_svc.store("db_password", "hunter2")
        client = TestClient(app)
        r = client.get("/vault/secret/db_password")
        assert r.status_code == 200
        data = r.json()
        assert data["found"] is True
        assert data["value"] == "hunter2"

    def test_api_get_missing_secret(self):
        client = TestClient(LaurenFactory.create(VaultModule))
        r = client.get("/vault/secret/nonexistent")
        assert r.status_code == 200
        assert r.json()["found"] is False

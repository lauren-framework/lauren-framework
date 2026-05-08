---
name: field-level-encryption
description: Encrypts and decrypts individual model fields using Fernet symmetric encryption (cryptography library). Use when storing PII, credentials, or secrets in a database that should be unreadable without the application key — and when you need safe key rotation without data loss.
---

> Use `codemap find "FieldEncryptor"` to check for existing encryption utilities before adding new ones.

# Field-Level Data Encryption & Key Rotation

`FieldEncryptor` wraps the `cryptography.fernet.Fernet` API and adds multi-key decryption for zero-downtime key rotation.

## FieldEncryptor

```python
from __future__ import annotations

from cryptography.fernet import Fernet, MultiFernet
from lauren import injectable, Scope

@injectable(scope=Scope.SINGLETON)
class FieldEncryptor:
    """Fernet-based field encryptor with key-rotation support.

    Encrypted tokens are URL-safe base64 strings that can be stored in any
    VARCHAR/TEXT column.  The token embeds a timestamp — Fernet.decrypt()
    optionally enforces a TTL if you pass ttl=<seconds>.
    """

    def __init__(self, primary_key: bytes | None = None) -> None:
        self._primary_key: bytes = primary_key or Fernet.generate_key()
        self._fernet: Fernet = Fernet(self._primary_key)
        self._old_keys: list[bytes] = []

    # ------------------------------------------------------------------
    # Core operations
    # ------------------------------------------------------------------

    def encrypt(self, value: str) -> str:
        """Encrypt a plaintext string and return a Fernet token (str)."""
        return self._fernet.encrypt(value.encode()).decode()

    def decrypt(self, token: str) -> str:
        """Decrypt a Fernet token.  Tries primary key first, then old keys."""
        if self._old_keys:
            multi = MultiFernet(
                [Fernet(k) for k in [self._primary_key] + self._old_keys]
            )
            return multi.decrypt(token.encode()).decode()
        return self._fernet.decrypt(token.encode()).decode()

    # ------------------------------------------------------------------
    # Key rotation
    # ------------------------------------------------------------------

    def rotate_key(self) -> bytes:
        """Generate a new primary key; old key moves to the fallback list.

        After rotating, call ``re_encrypt`` on all stored tokens to ensure
        they can be decrypted with the primary key alone (remove dependency
        on old keys).
        """
        self._old_keys.insert(0, self._primary_key)
        self._primary_key = Fernet.generate_key()
        self._fernet = Fernet(self._primary_key)
        return self._primary_key

    def re_encrypt(self, token: str) -> str:
        """Decrypt with any known key and re-encrypt with the current primary key."""
        plaintext = self.decrypt(token)
        return self._fernet.encrypt(plaintext.encode()).decode()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def generate_key() -> bytes:
        """Generate a random 32-byte Fernet key (URL-safe base64-encoded)."""
        return Fernet.generate_key()
```

## Usage in a service

```python
@injectable(scope=Scope.SINGLETON)
class UserService:
    def __init__(self, encryptor: FieldEncryptor) -> None:
        self._encryptor = encryptor
        self._users: dict[str, dict] = {}

    def create_user(self, user_id: str, ssn: str, email: str) -> None:
        self._users[user_id] = {
            "ssn_enc": self._encryptor.encrypt(ssn),      # encrypted at rest
            "email": email,                                 # plain-text OK
        }

    def get_ssn(self, user_id: str) -> str:
        return self._encryptor.decrypt(self._users[user_id]["ssn_enc"])
```

## Key rotation workflow

```python
# 1. Rotate the key (primary → old_keys, new primary generated)
new_key = encryptor.rotate_key()

# 2. Re-encrypt all tokens in persistent storage
for record in db.query("SELECT id, ssn_enc FROM users"):
    new_token = encryptor.re_encrypt(record.ssn_enc)
    db.execute("UPDATE users SET ssn_enc = ? WHERE id = ?", new_token, record.id)

# 3. Persist the new primary key in your secrets manager
secrets.put("FIELD_ENCRYPTION_KEY", new_key)

# 4. After all tokens are re-encrypted, old_keys can be cleared on the
#    next deploy (all tokens are now encrypted with the primary key).
```

## Module wiring

```python
from cryptography.fernet import Fernet
from lauren import LaurenFactory

ENCRYPTION_KEY = Fernet.generate_key()   # or load from env / secrets manager

@module(providers=[FieldEncryptor, UserService])
class AppModule:
    pass

app = LaurenFactory.create(AppModule)
```

To inject a specific key, use `use_value` / `use_factory`:

```python
from lauren._di.custom import use_factory

def make_encryptor() -> FieldEncryptor:
    key = os.environ["FIELD_ENCRYPTION_KEY"].encode()
    return FieldEncryptor(primary_key=key)

app = LaurenFactory.create(AppModule, global_providers=[use_factory(provide=FieldEncryptor, factory=make_encryptor)])
```

## Testing

```python
def test_encrypt_decrypt_roundtrip():
    enc = FieldEncryptor()
    token = enc.encrypt("secret")
    assert enc.decrypt(token) == "secret"

def test_key_rotation_old_tokens_still_decrypt():
    enc = FieldEncryptor()
    token = enc.encrypt("secret")
    enc.rotate_key()
    assert enc.decrypt(token) == "secret"   # old key still in fallback list
```

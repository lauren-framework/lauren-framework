# Stores

The async key-value `Store` behind sessions, revocation, the cache, idempotency keys and locks.

## The protocol

### `Store`

Async key-value store with per-key TTLs (values are bytes, keys are str).

``ttl`` is seconds as a ``float``; ``None`` means "no expiry" and a
value ``<= 0`` raises :class:`ValueError`. Every backend must pass the
shared contract suite in :mod:`lauren.testing.store_contract`.

A backend may also define ``async def startup(self)`` and
``async def aclose(self)``; :class:`~lauren.stores.StoresModule` and
``SessionConfig(store=...)`` run them with the app's lifecycle.

### `PrefixedStore`

A view of ``store`` whose keys are prefixed with ``"<prefix>:"``.

Views are cheap, share the underlying store's capacity, clock and
connection, and nest: ``PrefixedStore(s, "a").namespace("b")`` writes
``"a:b:<key>"``. A backend implements :meth:`Store.namespace` by
returning ``PrefixedStore(self, prefix)``.

## In-process store

### `MemoryStore`

Bounded, process-local :class:`~lauren.stores.Store`.

``max_entries`` is required (``ValueError`` when not a positive int): the
least recently used entries are evicted beyond it, so memory stays
bounded whatever the traffic. ``sweep_interval`` (seconds) bounds how
long expired-but-unread entries linger. ``clock`` is a zero-argument
callable returning seconds (``time.monotonic`` by default) — inject a
fake one to test TTLs without sleeping.

Fine for development, tests and single-worker deployments; share state
across workers with a networked backend (e.g. the ``lauren-redis``
companion) that implements the same protocol.

## Registration

### `StoresModule`

Register the app's key-value stores (import the module ``for_root`` returns).

### `StoreToken`

DI token of a named store: ``Annotated[Store, Inject(StoreToken("local"))]``.

Two ``StoreToken`` objects with the same name are the same token.

### `StoreRegistry`

Name-to-store lookup for the stores ``StoresModule`` registered.

The default store is available as ``registry.get()`` (name ``"default"``).

## Locks

### `Lock`

Async context manager holding the store key ``"lock:<name>"`` for at most ``ttl`` seconds.

``wait`` (seconds) is how long :meth:`acquire` keeps retrying, with
capped exponential backoff starting at ``poll_interval``; ``None`` or
``0`` tries once. ``async with`` raises :class:`TimeoutError` when the
lock could not be acquired. Pick a ``ttl`` longer than the critical
section: the lease is not extended automatically.

## Session adapters

### `StoreSessionStore`

```python
class StoreSessionStore(store: Store, namespace: str | None = 'session', serializer: SessionSerializer | None = None)
```

:class:`~lauren.sessions.SessionStore` over any :class:`~lauren.stores.Store`.

Session payloads are serialised with ``serializer`` (compact JSON by
default) and saved with ``ttl=max_age``. Keys live under
``"<namespace>:"`` (``"session"`` by default; ``None`` disables the
prefix).

#### `StoreSessionStore.new_id`

```python
def new_id(self) -> str
```

#### `StoreSessionStore.load`

```python
def load(self, session_id: str) -> dict[str, Any] | None
```

#### `StoreSessionStore.save`

```python
def save(self, session_id: str, data: dict[str, Any], max_age: int | None) -> None
```

#### `StoreSessionStore.delete`

```python
def delete(self, session_id: str) -> None
```

### `StoreRevocationStore`

```python
class StoreRevocationStore(store: Store, namespace: str | None = 'revocation')
```

:class:`~lauren.sessions.RevocationStore` over any :class:`~lauren.stores.Store`.

A revoked token is the key ``token:<id>``; a user cutoff is the key
``user:<id>`` holding the cutoff epoch. Both expire with their TTL, so
the index stays bounded by the backend. Keys live under
``"<namespace>:"`` (``"revocation"`` by default).

#### `StoreRevocationStore.revoke_token`

```python
def revoke_token(self, token_id: str, ttl: int | None = None) -> None
```

#### `StoreRevocationStore.is_token_revoked`

```python
def is_token_revoked(self, token_id: str) -> bool
```

#### `StoreRevocationStore.revoke_user`

```python
def revoke_user(self, user_id: str, cutoff: float | None = None, ttl: int | None = None) -> None
```

#### `StoreRevocationStore.user_cutoff`

```python
def user_cutoff(self, user_id: str) -> float | None
```

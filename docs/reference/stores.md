# Stores

The async key-value contract behind sessions, revocation, the cache,
idempotency keys and locks. See the [Stores & Caching guide](../guides/caching.md)
for the full story.

## The protocol

::: lauren.stores.Store

::: lauren.stores.PrefixedStore

## In-process store

::: lauren.stores.MemoryStore

## Registration

::: lauren.stores.StoresModule

::: lauren.stores.StoreToken

::: lauren.stores.StoreRegistry

## Locks

::: lauren.stores.Lock

## Session adapters

::: lauren.stores.StoreSessionStore

::: lauren.stores.StoreRevocationStore

## Contract suite

::: lauren.testing.store_contract.run_store_contract

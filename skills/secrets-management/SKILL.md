---
name: secrets-management
description: Provides a pluggable secrets provider interface with implementations for environment variables, in-memory (tests), HashiCorp Vault, and AWS Secrets Manager. Use when you need to inject secrets into Lauren services without hardcoding credentials.
---

> Use `codemap find "SymbolName"` to locate any symbol before reading — it gives
> exact file + line range and is faster than grep across the whole repo.

# Secrets Management (Vault / AWS Secrets Manager)

## Overview

Define a `SecretsProvider` abstract base class. Concrete implementations cover
environment variables, in-memory storage (tests), HashiCorp Vault, and AWS
Secrets Manager. The `SecretsService` singleton wraps the provider and is
injected anywhere a secret is needed. Swap backends via `use_value`.

## Abstract interface

```python
from abc import ABC, abstractmethod

class SecretsProvider(ABC):
    @abstractmethod
    def get_secret(self, name: str) -> str: ...
```

## EnvSecretsProvider

```python
import os

class EnvSecretsProvider(SecretsProvider):
    def get_secret(self, name: str) -> str:
        val = os.getenv(name)
        if val is None:
            raise KeyError(f"Secret '{name}' not found in environment")
        return val
```

## InMemorySecretsProvider (tests)

```python
class InMemorySecretsProvider(SecretsProvider):
    def __init__(self, secrets: dict[str, str] | None = None) -> None:
        self._secrets: dict[str, str] = secrets or {}

    def set_secret(self, name: str, value: str) -> None:
        self._secrets[name] = value

    def get_secret(self, name: str) -> str:
        if name not in self._secrets:
            raise KeyError(f"Secret '{name}' not found")
        return self._secrets[name]
```

## HashiCorp Vault implementation

```python
import hvac

class VaultSecretsProvider(SecretsProvider):
    def __init__(self, url: str, token: str, mount: str = "secret") -> None:
        self._client = hvac.Client(url=url, token=token)
        self._mount = mount

    def get_secret(self, name: str) -> str:
        path, key = name.rsplit("/", 1) if "/" in name else (name, "value")
        resp = self._client.secrets.kv.v2.read_secret_version(
            path=path, mount_point=self._mount
        )
        return resp["data"]["data"][key]
```

## AWS Secrets Manager implementation

```python
import boto3, json

class AWSSecretsProvider(SecretsProvider):
    def __init__(self, region: str = "us-east-1") -> None:
        self._client = boto3.client("secretsmanager", region_name=region)

    def get_secret(self, name: str) -> str:
        resp = self._client.get_secret_value(SecretId=name)
        payload = resp.get("SecretString") or resp["SecretBinary"].decode()
        try:
            return json.loads(payload)["value"]
        except (json.JSONDecodeError, KeyError):
            return payload
```

## SecretsService

```python
from lauren import injectable, Scope

@injectable(scope=Scope.SINGLETON)
class SecretsService:
    def __init__(self, provider: SecretsProvider) -> None:
        self._provider = provider

    def get(self, name: str) -> str:
        return self._provider.get_secret(name)
```

## Wiring via use_value

```python
from lauren import module, use_value

# Tests
provider = InMemorySecretsProvider({"DB_PASSWORD": "secret123", "API_KEY": "test"})

@module(
    providers=[
        use_value(provide=SecretsProvider, value=provider),
        SecretsService,
    ],
)
class TestSecretsModule:
    pass

# Production — Vault
vault = VaultSecretsProvider(url="https://vault:8200", token="s.xxxx")

@module(
    providers=[
        use_value(provide=SecretsProvider, value=vault),
        SecretsService,
    ],
)
class ProdSecretsModule:
    pass
```

## Using SecretsService in another service

```python
@injectable(scope=Scope.SINGLETON)
class DatabaseService:
    def __init__(self, secrets: SecretsService) -> None:
        password = secrets.get("DB_PASSWORD")
        self._engine = create_engine(f"postgresql://user:{password}@host/db")
```

## Caching secrets

Vault/AWS calls are network I/O — cache secrets in the `SINGLETON` after first
fetch to avoid per-request latency:

```python
@injectable(scope=Scope.SINGLETON)
class CachingSecretsService:
    def __init__(self, provider: SecretsProvider) -> None:
        self._provider = provider
        self._cache: dict[str, str] = {}

    def get(self, name: str) -> str:
        if name not in self._cache:
            self._cache[name] = self._provider.get_secret(name)
        return self._cache[name]
```

## Common mistakes

- Logging secret values — never pass `secrets.get(...)` to a logger.
- Constructing the provider inside `__init__` with hardcoded credentials —
  read the token/URL from environment variables instead.
- Not raising on missing secrets — silent `None` return causes confusing
  errors later; always raise `KeyError` or a custom `SecretNotFoundError`.

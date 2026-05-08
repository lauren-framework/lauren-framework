---
name: pydantic-settings-config
description: Provides centralized application configuration using Pydantic BaseSettings (or BaseModel with env reading). Use when you need typed, validated configuration loaded from environment variables and injected into Lauren services.
---

> Use `codemap find "SymbolName"` to locate any symbol before reading — it gives
> exact file + line range and is faster than grep across the whole repo.

# Centralized Config Service with Pydantic Settings

## Overview

`AppConfig` validates and stores application settings. `ConfigService` wraps it
as a DI singleton so any service can declare `cfg: ConfigService` and read
typed values without touching `os.environ` directly. Two approaches are shown:
`pydantic-settings` (preferred) and a plain `pydantic.BaseModel` with manual
env reading (works without the extra package).

## Approach A — pydantic-settings (preferred)

```python
from pydantic_settings import BaseSettings, SettingsConfigDict

class AppConfig(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "sqlite:///:memory:"
    secret_key: str = "default-secret"
    debug: bool = False
    api_prefix: str = "/api/v1"
    cors_origins: list[str] = ["*"]
```

`BaseSettings` reads from environment variables automatically, case-insensitively.
Nested models are supported via `env_nested_delimiter="_"`.

## Approach B — plain pydantic.BaseModel (no extra dep)

```python
import os
from pydantic import BaseModel

class AppConfig(BaseModel):
    database_url: str = "sqlite:///:memory:"
    secret_key: str = "default-secret"
    debug: bool = False
    api_prefix: str = "/api/v1"

    @classmethod
    def from_env(cls) -> "AppConfig":
        return cls(
            database_url=os.getenv("DATABASE_URL", "sqlite:///:memory:"),
            secret_key=os.getenv("SECRET_KEY", "default-secret"),
            debug=os.getenv("DEBUG", "false").lower() == "true",
            api_prefix=os.getenv("API_PREFIX", "/api/v1"),
        )
```

## ConfigService

```python
from lauren import injectable, Scope

@injectable(scope=Scope.SINGLETON)
class ConfigService:
    def __init__(self) -> None:
        self._config = AppConfig.from_env()   # or AppConfig() with pydantic-settings

    @property
    def config(self) -> AppConfig:
        return self._config

    def get(self, key: str, default=None):
        return getattr(self._config, key, default)
```

## Injecting into services

```python
@injectable(scope=Scope.SINGLETON)
class DatabaseService:
    def __init__(self, cfg: ConfigService) -> None:
        self._engine = create_engine(cfg.config.database_url)
```

## Module wiring

```python
from lauren import module

@module(
    controllers=[AppController],
    providers=[ConfigService, DatabaseService],
)
class AppModule:
    pass
```

## Overriding config in tests

```python
import os
from lauren import use_value

# Option 1: patch env vars before building app
os.environ["DEBUG"] = "true"
client = TestClient(LaurenFactory.create(AppModule))

# Option 2: supply a pre-built ConfigService via use_value
cfg_svc = ConfigService.__new__(ConfigService)
cfg_svc._config = AppConfig(debug=True, secret_key="test-key")

@module(providers=[use_value(provide=ConfigService, value=cfg_svc)])
class TestModule:
    pass
```

## Validation with Pydantic validators

```python
from pydantic import field_validator

class AppConfig(BaseModel):
    secret_key: str = "default-secret"
    min_password_length: int = 8

    @field_validator("secret_key")
    @classmethod
    def secret_key_not_default_in_prod(cls, v: str) -> str:
        import os
        if os.getenv("APP_ENV") == "production" and v == "default-secret":
            raise ValueError("SECRET_KEY must be set in production")
        return v
```

## Common mistakes

- Reading `os.environ` at import time — values may not yet be set. Always read
  inside `__init__` or `from_env()`.
- Not exporting `ConfigService` from its module — other modules won't resolve it.
- Storing secrets (passwords, API keys) in `AppConfig` fields that appear in
  logs or `model_dump()` output — use `SecretStr` type in pydantic to mask them.

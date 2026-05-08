---
name: environment-profiles
description: Merges base configuration with environment-specific overrides (dev/staging/prod). Use when different deployment environments need different settings loaded from a profile name rather than individual env vars.
---

> Use `codemap find "SymbolName"` to locate any symbol before reading — it gives
> exact file + line range and is faster than grep across the whole repo.

# Environment Profile Merging (dev/staging/prod)

## Overview

Define a `BASE_CONFIG` dict and a `PROFILE_OVERRIDES` mapping from profile name
to partial dict. `ProfileConfigService` reads `APP_ENV` at construction, merges
the appropriate overrides, and exposes typed accessors. Pure Python — no
external dependencies.

## Configuration dictionaries

```python
from typing import Any

BASE_CONFIG: dict[str, Any] = {
    "debug": False,
    "log_level": "INFO",
    "database_pool_size": 10,
    "cache_ttl": 300,
    "allowed_hosts": ["*"],
}

PROFILE_OVERRIDES: dict[str, dict[str, Any]] = {
    "development": {
        "debug": True,
        "log_level": "DEBUG",
        "database_pool_size": 2,
    },
    "staging": {
        "log_level": "WARNING",
        "database_pool_size": 5,
    },
    "production": {
        "database_pool_size": 20,
        "cache_ttl": 3600,
        "allowed_hosts": ["myapp.com", "api.myapp.com"],
    },
}
```

## ProfileConfigService

```python
import os
from typing import Any
from lauren import injectable, Scope

@injectable(scope=Scope.SINGLETON)
class ProfileConfigService:
    def __init__(self) -> None:
        profile = os.getenv("APP_ENV", "development")
        self._config: dict[str, Any] = {
            **BASE_CONFIG,
            **PROFILE_OVERRIDES.get(profile, {}),
        }
        self._profile = profile

    @property
    def profile(self) -> str:
        return self._profile

    def get(self, key: str, default: Any = None) -> Any:
        return self._config.get(key, default)

    def all(self) -> dict[str, Any]:
        return dict(self._config)
```

## Layered overrides (file + env)

For more sophisticated setups, add a third layer of env-var overrides on top:

```python
import os, json

@injectable(scope=Scope.SINGLETON)
class LayeredConfigService:
    def __init__(self) -> None:
        profile = os.getenv("APP_ENV", "development")
        merged = {**BASE_CONFIG, **PROFILE_OVERRIDES.get(profile, {})}

        # Highest priority: individual env vars (uppercased key names)
        for key in merged:
            env_val = os.getenv(key.upper())
            if env_val is not None:
                try:
                    merged[key] = json.loads(env_val)  # parse booleans/ints
                except json.JSONDecodeError:
                    merged[key] = env_val

        self._config = merged
        self._profile = profile
```

## Controller exposing profile info

```python
from lauren import controller, get, module

@controller("/profile")
class ProfileController:
    def __init__(self, cfg: ProfileConfigService) -> None:
        self._cfg = cfg

    @get("/")
    async def current_profile(self) -> dict:
        return {"profile": self._cfg.profile, "config": self._cfg.all()}

@module(controllers=[ProfileController], providers=[ProfileConfigService])
class ProfileModule:
    pass
```

## Testing with different profiles

```python
import os

def build_app_for_profile(profile: str):
    os.environ["APP_ENV"] = profile
    try:
        return TestClient(LaurenFactory.create(ProfileModule))
    finally:
        del os.environ["APP_ENV"]
```

## Loading from YAML or TOML (advanced)

```python
import tomllib, pathlib

def load_profile(profile: str) -> dict:
    base = tomllib.loads(pathlib.Path("config/base.toml").read_text())
    overrides_path = pathlib.Path(f"config/{profile}.toml")
    overrides = tomllib.loads(overrides_path.read_text()) if overrides_path.exists() else {}
    return {**base, **overrides}
```

## Common mistakes

- Reading `APP_ENV` at module import time — use `os.getenv` inside `__init__`
  so tests can set the env var before construction.
- Mutating `BASE_CONFIG` at runtime — always shallow-copy before merging
  (`{**BASE_CONFIG, ...}`) so the original stays clean.
- Deep-nested config merging with `{**base, **overrides}` — this only merges
  top-level keys; nested dicts are replaced entirely. Use `deepmerge` or
  a recursive merge if nested overrides are needed.
- Forgetting to export `ProfileConfigService` when multiple modules need it —
  add it to the module's `exports=[]` list.
